"""Tab: Credentials — KeePass credential manager UI.

NOTE: pykeepass imports MUST stay inside render() to avoid breaking the portal
on startup if the dependency is not yet installed.
"""

import streamlit as st


def render():
    import json
    import os
    import time
    from pathlib import Path

    from pykeepass import PyKeePass, create_database
    from scripts.keepass import get_credential_entry as _ph_get_entry
    from scripts.keepass import store_credential as _ph_store_credential

    KEEPASS_DIR = Path("/home/agent/.keepass")
    DB_PATH = KEEPASS_DIR / "credentials.kdbx"
    CLAUDE_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"

    def _update_claude_settings_env(var_name: str, value: str) -> tuple[bool, str]:
        """Write `env[var_name] = value` into ~/.claude/settings.json, preserving other keys."""
        try:
            CLAUDE_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            if CLAUDE_SETTINGS_PATH.exists():
                raw = CLAUDE_SETTINGS_PATH.read_text()
                data = json.loads(raw) if raw.strip() else {}
            else:
                data = {}
            env = data.setdefault("env", {})
            env[var_name] = value
            tmp_path = CLAUDE_SETTINGS_PATH.with_suffix(".json.tmp")
            tmp_path.write_text(json.dumps(data, indent=2) + "\n")
            os.replace(tmp_path, CLAUDE_SETTINGS_PATH)
            return True, str(CLAUDE_SETTINGS_PATH)
        except Exception as exc:
            return False, str(exc)

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

    # ── PostHog API key ────────────────────────────────────────────
    st.divider()
    st.subheader("PostHog API key")

    PH_KEEPASS_TITLE = "POSTHOG_API_KEY_1"
    PH_KEEPASS_GROUP = "API Keys"
    PH_ENV_VAR = "POSTHOG_API_KEY"

    ph_cache_ts = st.session_state.get("ph_cred_check_ts", 0)
    if time.time() - ph_cache_ts > 60 or "ph_cred_data" not in st.session_state:
        st.session_state["ph_cred_data"] = _ph_get_entry(PH_KEEPASS_TITLE)
        st.session_state["ph_cred_check_ts"] = time.time()

    ph_cred = st.session_state.get("ph_cred_data")

    def _ph_save(label: str, key: str) -> None:
        saved_ok = _ph_store_credential(
            PH_KEEPASS_TITLE,
            label,
            key,
            group=PH_KEEPASS_GROUP,
        )
        if not saved_ok:
            st.error("Failed to save key to KeePass.")
            return
        ok, detail = _update_claude_settings_env(PH_ENV_VAR, key)
        if ok:
            st.success(
                "Key saved to KeePass and written to `~/.claude/settings.json`. "
                "The posthog MCP server will pick it up on the next Claude Code session."
            )
        else:
            st.warning(
                f"Key saved to KeePass, but failed to update `~/.claude/settings.json`: {detail}"
            )
        for k in ("ph_cred_data", "ph_cred_check_ts", "ph_replace"):
            st.session_state.pop(k, None)
        st.rerun()

    if ph_cred is not None:
        ph_label = ph_cred.get("username", "")
        st.success(f"PostHog API key stored for **{ph_label}**.")

        if st.button("Replace Key", key="ph_replace_btn"):
            st.session_state["ph_replace"] = True
            st.rerun()

        if st.session_state.get("ph_replace", False):
            with st.form("ph_replace_form", clear_on_submit=True):
                new_ph_label = st.text_input("Label / account", value=ph_label)
                new_ph_key = st.text_input("New API key", type="password")
                replace_submitted = st.form_submit_button("Save")
            if replace_submitted:
                if not new_ph_label.strip() or not new_ph_key.strip():
                    st.error("Label and API key are required.")
                else:
                    _ph_save(new_ph_label.strip(), new_ph_key.strip())
    else:
        st.caption(
            "Paste a PostHog Personal API key. It will be saved to KeePass and "
            "written to `~/.claude/settings.json` so the `posthog` MCP server "
            "picks it up on the next Claude Code session."
        )
        with st.form("ph_add_form", clear_on_submit=True):
            ph_label = st.text_input("Label / account")
            ph_key = st.text_input("Personal API key", type="password")
            ph_submitted = st.form_submit_button("Save")
        if ph_submitted:
            if not ph_label.strip() or not ph_key.strip():
                st.error("Label and API key are required.")
            else:
                _ph_save(ph_label.strip(), ph_key.strip())

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
