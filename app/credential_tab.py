"""Tab: Credentials — KeePass credential manager UI.

NOTE: pykeepass imports MUST stay inside render() to avoid breaking the portal
on startup if the dependency is not yet installed.
"""

import streamlit as st


def render():
    import json
    import subprocess
    import time
    from pathlib import Path

    from pykeepass import PyKeePass, create_database
    from scripts.keepass import get_credential_entry as _gh_get_entry
    from scripts.keepass import store_credential as _gh_store_credential

    KEEPASS_DIR = Path("/home/agent/.keepass")
    DB_PATH = KEEPASS_DIR / "credentials.kdbx"
    MCP_JSON_PATH = Path(__file__).resolve().parents[1] / ".mcp.json"

    def _update_mcp_json_token(token: str) -> bool:
        """Write the PAT into .mcp.json, replacing any existing Authorization value."""
        try:
            data = json.loads(MCP_JSON_PATH.read_text())
            data["mcpServers"]["github"]["headers"]["Authorization"] = f"Bearer {token}"
            MCP_JSON_PATH.write_text(json.dumps(data, indent=2) + "\n")
            return True
        except Exception:
            return False

    # ── Init guard ────────────────────────────────────────────────
    if not DB_PATH.exists():
        st.info("No credential database found. Initialize one to get started.")
        if st.button("Initialize Database"):
            try:
                KEEPASS_DIR.mkdir(parents=True, exist_ok=True)
                kp = create_database(str(DB_PATH), password="")
                kp.save()
                st.success(f"Database created at `{DB_PATH}`")
                st.rerun()
            except Exception as exc:
                st.error(f"Failed to create database: {exc}")
        return

    # ── Open database ─────────────────────────────────────────────
    try:
        kp = PyKeePass(str(DB_PATH), password="")
    except Exception as exc:
        st.error(f"Failed to open credential database: {exc}")
        return

    entries = list(kp.entries) if kp.entries else []
    groups = [g for g in kp.groups if not g.is_root_group]

    def _resolve_group(grp_sel, new_grp_name):
        """Resolve a group selection to a pykeepass group, creating if needed."""
        grp_path = (
            new_grp_name
            if grp_sel == "+ New group"
            else (grp_sel if grp_sel != "(none)" else "")
        )
        if not grp_path:
            return kp.root_group
        parts = [p.strip() for p in grp_path.split("/") if p.strip()]
        cur = kp.root_group
        for part in parts:
            found = kp.find_groups(name=part, group=cur, recursive=False, first=True)
            cur = found if found else kp.add_group(cur, part)
        return cur

    group_names = sorted({"/".join(g.path) for g in groups if g.path}) if groups else []
    group_options = ["(none)"] + group_names + ["+ New group"]

    # ── Metrics strip ─────────────────────────────────────────────
    c1, c2 = st.columns(2)
    c1.metric("Entries", len(entries))
    c2.metric("Groups", len(groups))

    # ── Search / filter ───────────────────────────────────────────
    search = st.text_input(
        "Search credentials", placeholder="Filter by title or username..."
    )

    # ── Credential list ───────────────────────────────────────────
    filtered = entries
    if search and len(search) >= 2:
        q = search.lower()
        filtered = [
            e
            for e in entries
            if (e.title and q in e.title.lower())
            or (e.username and q in e.username.lower())
            or (e.url and q in e.url.lower())
        ]

    if not filtered:
        st.caption(
            "No credentials found."
            if search
            else "No credentials stored yet. Add one below."
        )
    else:
        for entry in filtered:
            uid = str(entry.uuid)
            group_path = (
                "/".join(entry.group.path) if entry.group and entry.group.path else ""
            )
            label = entry.title or "(untitled)"
            if group_path:
                label += f"  [{group_path}]"

            with st.expander(label, expanded=False):
                st.text(f"Username: {entry.username or '—'}")
                if entry.url:
                    st.text(f"URL: {entry.url}")
                if group_path:
                    st.text(f"Group: {group_path}")
                if entry.notes:
                    st.text(f"Notes: {entry.notes}")

                # Password with show/hide toggle
                pw_key = f"cred_show_pw_{uid}"
                col_pw, col_btn = st.columns([4, 1])
                with col_pw:
                    if st.session_state.get(pw_key, False):
                        st.code(entry.password or "", language=None)
                    else:
                        st.text("••••••••••••")
                with col_btn:
                    btn_label = (
                        "Hide" if st.session_state.get(pw_key, False) else "Show"
                    )
                    if st.button(btn_label, key=f"cred_toggle_{uid}"):
                        st.session_state[pw_key] = not st.session_state.get(
                            pw_key, False
                        )
                        st.rerun()

                # Edit / Delete — mutually exclusive states
                edit_key = f"cred_edit_{uid}"
                del_key = f"cred_confirm_del_{uid}"
                edit_widget_keys = [
                    edit_key,
                    f"cred_etitle_{uid}",
                    f"cred_euser_{uid}",
                    f"cred_epass_{uid}",
                    f"cred_eurl_{uid}",
                    f"cred_enotes_{uid}",
                    f"cred_egrp_{uid}",
                    f"cred_enewgrp_{uid}",
                ]

                if st.session_state.get(edit_key, False):
                    # ── Edit form
                    st.markdown("---")
                    new_title = st.text_input(
                        "Title", value=entry.title or "", key=f"cred_etitle_{uid}"
                    )
                    new_username = st.text_input(
                        "Username", value=entry.username or "", key=f"cred_euser_{uid}"
                    )
                    new_password = st.text_input(
                        "Password",
                        value=entry.password or "",
                        type="password",
                        key=f"cred_epass_{uid}",
                    )
                    new_url = st.text_input(
                        "URL", value=entry.url or "", key=f"cred_eurl_{uid}"
                    )
                    new_notes = st.text_area(
                        "Notes",
                        value=entry.notes or "",
                        height=68,
                        key=f"cred_enotes_{uid}",
                    )
                    cur_group = (
                        "/".join(entry.group.path)
                        if entry.group and entry.group.path
                        else "(none)"
                    )
                    default_idx = (
                        group_options.index(cur_group)
                        if cur_group in group_options
                        else 0
                    )
                    edit_grp_sel = st.selectbox(
                        "Group",
                        group_options,
                        index=default_idx,
                        key=f"cred_egrp_{uid}",
                    )
                    edit_new_grp = ""
                    if edit_grp_sel == "+ New group":
                        edit_new_grp = st.text_input(
                            "New group name", key=f"cred_enewgrp_{uid}"
                        )
                    ec1, ec2 = st.columns(2)
                    if ec1.button("Save", key=f"cred_esave_{uid}"):
                        if not new_title:
                            st.error("Title cannot be empty.")
                        else:
                            try:
                                dest = _resolve_group(edit_grp_sel, edit_new_grp)
                                # Check duplicate title in target group (skip self)
                                dup = kp.find_entries(
                                    title=new_title,
                                    group=dest,
                                    recursive=False,
                                    first=True,
                                )
                                if dup and dup.uuid != entry.uuid:
                                    st.error(
                                        f"An entry titled '{new_title}' already exists in this group."
                                    )
                                else:
                                    entry.title = new_title
                                    entry.username = new_username
                                    entry.password = new_password
                                    entry.url = new_url or ""
                                    entry.notes = new_notes or ""
                                    if entry.group != dest:
                                        kp.move_entry(entry, dest)
                                    kp.save()
                                    for k in edit_widget_keys:
                                        st.session_state.pop(k, None)
                                    st.rerun()
                            except Exception as exc:
                                st.error(f"Failed to save: {exc}")
                    if ec2.button("Cancel", key=f"cred_ecancel_{uid}"):
                        for k in edit_widget_keys:
                            st.session_state.pop(k, None)
                        st.rerun()

                elif st.session_state.get(del_key, False):
                    # ── Delete confirmation
                    st.warning(f"Delete '{entry.title}'? This cannot be undone.")
                    dc1, dc2 = st.columns(2)
                    if dc1.button("Confirm Delete", key=f"cred_del_yes_{uid}"):
                        try:
                            kp.delete_entry(entry)
                            kp.save()
                            st.session_state.pop(del_key, None)
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Delete failed: {exc}")
                    if dc2.button("Cancel", key=f"cred_del_no_{uid}"):
                        st.session_state.pop(del_key, None)
                        st.rerun()

                else:
                    # ── Action buttons
                    act1, act2 = st.columns(2)
                    if act1.button("Edit", key=f"cred_edit_btn_{uid}"):
                        st.session_state[edit_key] = True
                        st.rerun()
                    if act2.button("Delete", key=f"cred_del_{uid}"):
                        st.session_state[del_key] = True
                        st.rerun()

    # ── SSH Key section ─────────────────────────────────────────────
    st.divider()
    st.subheader("SSH Key")

    SSH_DIR = Path("/home/agent/.ssh")
    SSH_KEY_PATH = SSH_DIR / "id_ed25519"

    if SSH_KEY_PATH.exists():
        st.success("SSH private key is installed.")
        if st.button("Replace SSH Key"):
            st.session_state["cred_ssh_replace"] = True
            st.rerun()

        if st.session_state.get("cred_ssh_replace", False):
            with st.form("ssh_replace", clear_on_submit=True):
                new_key = st.text_area(
                    "Paste new id_ed25519 private key",
                    height=150,
                    placeholder="-----BEGIN OPENSSH PRIVATE KEY-----\n...\n-----END OPENSSH PRIVATE KEY-----",
                )
                replace_submitted = st.form_submit_button("Save Key")
            if replace_submitted:
                if not new_key or not new_key.strip():
                    st.error("Private key cannot be empty.")
                else:
                    try:
                        SSH_DIR.mkdir(parents=True, exist_ok=True)
                        SSH_KEY_PATH.write_text(new_key.strip() + "\n")
                        SSH_KEY_PATH.chmod(0o400)
                        st.session_state.pop("cred_ssh_replace", None)
                        st.success("SSH key replaced.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Failed to save SSH key: {exc}")
    else:
        st.caption("No SSH key installed. Paste your `id_ed25519` private key below.")
        with st.form("ssh_add", clear_on_submit=True):
            ssh_key = st.text_area(
                "Paste id_ed25519 private key",
                height=150,
                placeholder="-----BEGIN OPENSSH PRIVATE KEY-----\n...\n-----END OPENSSH PRIVATE KEY-----",
            )
            ssh_submitted = st.form_submit_button("Save Key")
        if ssh_submitted:
            if not ssh_key or not ssh_key.strip():
                st.error("Private key cannot be empty.")
            else:
                try:
                    SSH_DIR.mkdir(parents=True, exist_ok=True)
                    SSH_KEY_PATH.write_text(ssh_key.strip() + "\n")
                    SSH_KEY_PATH.chmod(0o400)
                    st.success("SSH key saved.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Failed to save SSH key: {exc}")

    # ── GitHub CLI (gh) section ────────────────────────────────────
    st.divider()
    st.subheader("GitHub CLI (gh)")

    GH_KEEPASS_TITLE = "GITHUB_TOKEN_1"
    GH_KEEPASS_GROUP = "API Keys"

    # Check for existing credential (cached 60s)
    gh_cache_ts = st.session_state.get("gh_cred_check_ts", 0)
    if time.time() - gh_cache_ts > 60 or "gh_cred_data" not in st.session_state:
        st.session_state["gh_cred_data"] = _gh_get_entry(GH_KEEPASS_TITLE)
        st.session_state["gh_cred_check_ts"] = time.time()

    gh_cred = st.session_state.get("gh_cred_data")

    if gh_cred is not None:
        gh_username = gh_cred.get("username", "")
        st.success(f"GitHub token stored for **{gh_username}**.")

        # Test button
        if st.button("Test gh auth", key="gh_test_btn"):
            try:
                test_result = subprocess.run(
                    ["gh", "auth", "status"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                output = test_result.stdout or test_result.stderr
                if test_result.returncode == 0:
                    st.success(output.strip())
                else:
                    st.error(
                        output.strip()
                        or "gh auth status returned a non-zero exit code."
                    )
            except FileNotFoundError:
                st.error("`gh` is not installed or not in PATH.")
            except subprocess.TimeoutExpired:
                st.error("gh auth status timed out.")
            except Exception as exc:
                st.error(f"Error: {exc}")

        # Replace token
        if st.button("Replace Token", key="gh_replace_btn"):
            st.session_state["gh_replace"] = True
            st.rerun()

        if st.session_state.get("gh_replace", False):
            with st.form("gh_replace_form", clear_on_submit=True):
                new_gh_user = st.text_input("GitHub Username", value=gh_username)
                new_gh_pat = st.text_input(
                    "New Personal Access Token (PAT)", type="password"
                )
                replace_submitted = st.form_submit_button("Save & Login")
            if replace_submitted:
                if not new_gh_user.strip() or not new_gh_pat.strip():
                    st.error("Username and PAT are required.")
                else:
                    saved_ok = _gh_store_credential(
                        GH_KEEPASS_TITLE,
                        new_gh_user.strip(),
                        new_gh_pat.strip(),
                        group=GH_KEEPASS_GROUP,
                    )
                    if not saved_ok:
                        st.error("Failed to save token to KeePass.")
                    else:
                        if not _update_mcp_json_token(new_gh_pat.strip()):
                            st.warning("Token saved, but failed to update `.mcp.json`.")
                        try:
                            login_result = subprocess.run(
                                ["gh", "auth", "login", "--with-token"],
                                input=new_gh_pat.strip(),
                                capture_output=True,
                                text=True,
                                timeout=15,
                            )
                            if login_result.returncode == 0:
                                for k in (
                                    "gh_cred_data",
                                    "gh_cred_check_ts",
                                    "gh_replace",
                                ):
                                    st.session_state.pop(k, None)
                                st.success("Token saved and gh CLI authenticated.")
                                st.rerun()
                            else:
                                for k in (
                                    "gh_cred_data",
                                    "gh_cred_check_ts",
                                    "gh_replace",
                                ):
                                    st.session_state.pop(k, None)
                                st.error(
                                    f"Token saved but gh login failed: {login_result.stderr.strip()}"
                                )
                        except FileNotFoundError:
                            st.session_state.pop("gh_cred_data", None)
                            st.session_state.pop("gh_cred_check_ts", None)
                            st.session_state.pop("gh_replace", None)
                            st.warning(
                                "Token saved to KeePass, but `gh` is not installed or not in PATH."
                            )
                        except Exception as exc:
                            st.session_state.pop("gh_cred_data", None)
                            st.session_state.pop("gh_cred_check_ts", None)
                            st.session_state.pop("gh_replace", None)
                            st.error(f"Token saved but gh login error: {exc}")

    else:
        st.caption(
            "Paste a GitHub Personal Access Token to save it to KeePass and authenticate the `gh` CLI. "
            "Create one at **GitHub → Settings → Developer settings → Personal access tokens** "
            "(scopes: `repo`, `read:org`, `workflow`)."
        )
        with st.form("gh_add_form", clear_on_submit=True):
            gh_user = st.text_input("GitHub Username")
            gh_pat = st.text_input("Personal Access Token (PAT)", type="password")
            gh_submitted = st.form_submit_button("Save & Login")

        if gh_submitted:
            if not gh_user.strip() or not gh_pat.strip():
                st.error("Username and PAT are required.")
            else:
                saved_ok = _gh_store_credential(
                    GH_KEEPASS_TITLE,
                    gh_user.strip(),
                    gh_pat.strip(),
                    group=GH_KEEPASS_GROUP,
                )
                if not saved_ok:
                    st.error("Failed to save token to KeePass.")
                else:
                    if not _update_mcp_json_token(gh_pat.strip()):
                        st.warning("Token saved, but failed to update `.mcp.json`.")
                    try:
                        login_result = subprocess.run(
                            ["gh", "auth", "login", "--with-token"],
                            input=gh_pat.strip(),
                            capture_output=True,
                            text=True,
                            timeout=15,
                        )
                        st.session_state.pop("gh_cred_data", None)
                        st.session_state.pop("gh_cred_check_ts", None)
                        if login_result.returncode == 0:
                            st.success("Token saved and gh CLI authenticated.")
                        else:
                            st.error(
                                f"Token saved but gh login failed: {login_result.stderr.strip()}"
                            )
                        st.rerun()
                    except FileNotFoundError:
                        st.session_state.pop("gh_cred_data", None)
                        st.session_state.pop("gh_cred_check_ts", None)
                        st.warning(
                            "Token saved to KeePass, but `gh` is not installed or not in PATH."
                        )
                        st.rerun()
                    except Exception as exc:
                        st.session_state.pop("gh_cred_data", None)
                        st.session_state.pop("gh_cred_check_ts", None)
                        st.error(f"Token saved but gh login error: {exc}")
                        st.rerun()

    # ── Replace Database ───────────────────────────────────────────
    st.divider()
    st.subheader("Replace Database")
    st.caption(
        "Upload a `.kdbx` file to replace the current credential database. The file must be openable without a password or keyfile."
    )

    uploaded = st.file_uploader(
        "Upload .kdbx file", type=["kdbx"], key="cred_upload_kdbx"
    )
    if uploaded is not None:
        import tempfile

        # Validate the uploaded file by trying to open it
        tmp_path = None
        valid = False
        try:
            with tempfile.NamedTemporaryFile(suffix=".kdbx", delete=False) as tmp:
                tmp.write(uploaded.getvalue())
                tmp_path = tmp.name
            PyKeePass(tmp_path, password="")
            valid = True
        except Exception:
            st.error(
                "Invalid `.kdbx` file. The file is either corrupted or requires a password/keyfile to open."
            )
        finally:
            if tmp_path:
                Path(tmp_path).unlink(missing_ok=True)

        if valid:
            st.success("File is valid and can be opened without a password.")
            confirm_text = st.text_input(
                'Type "confirm" to replace the existing database',
                key="cred_replace_confirm",
            )
            if st.button("Replace Database", key="cred_replace_btn"):
                if confirm_text.strip().lower() != "confirm":
                    st.error('You must type "confirm" to proceed.')
                else:
                    try:
                        # Backup existing DB
                        if DB_PATH.exists():
                            backup_path = DB_PATH.with_suffix(".kdbx.bak")
                            backup_path.write_bytes(DB_PATH.read_bytes())
                        # Write uploaded file
                        DB_PATH.write_bytes(uploaded.getvalue())
                        st.success("Database replaced successfully.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Failed to replace database: {exc}")

    # ── Add credential form ───────────────────────────────────────
    st.divider()
    st.subheader("Add Credential")

    with st.form("cred_add", clear_on_submit=True):
        title = st.text_input("Title *")
        username = st.text_input("Username *")
        password = st.text_input("Password *", type="password")
        url = st.text_input("URL")
        notes = st.text_area("Notes", height=68)
        group_sel = st.selectbox("Group", group_options)
        new_group = ""
        if group_sel == "+ New group":
            new_group = st.text_input(
                "New group name (use / for nesting, e.g. Services/AWS)"
            )
        submitted = st.form_submit_button("Save")

    if submitted:
        if not title or not username or not password:
            st.error("Title, Username, and Password are required.")
        else:
            try:
                dest = _resolve_group(group_sel, new_group)

                # Check for duplicate title in target group
                existing = kp.find_entries(
                    title=title, group=dest, recursive=False, first=True
                )
                if existing:
                    st.error(
                        f"An entry titled '{title}' already exists in this group. Use a different title."
                    )
                    return

                kp.add_entry(
                    dest,
                    title=title,
                    username=username,
                    password=password,
                    url=url or "",
                    notes=notes or "",
                )
                kp.save()
                st.success(f"Saved '{title}'")
                st.rerun()
            except Exception as exc:
                st.error(f"Failed to save: {exc}")
