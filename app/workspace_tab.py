"""Tab: Workspace — file upload + iframe to Caddy file browser."""

import os
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

WORKSPACE_ROOT = Path("/agent/workspace")


def _safe_subdir(subdir: str) -> Path | None:
    """Resolve subdir under WORKSPACE_ROOT. Return None if path traversal detected."""
    if not subdir:
        return WORKSPACE_ROOT
    target = (WORKSPACE_ROOT / subdir).resolve()
    if not target.is_relative_to(WORKSPACE_ROOT.resolve()):
        return None
    return target


def render():
    st.subheader("Workspace")
    st.caption(
        "Browse and manage files in /agent/workspace/ via the Caddy file browser."
    )

    with st.expander("Upload files", icon=":material/upload:"):
        subdir = st.text_input(
            "Subdirectory (optional)",
            placeholder="e.g. data/input",
            help="Leave empty to upload to the workspace root.",
        )
        uploaded = st.file_uploader(
            "Choose files",
            accept_multiple_files=True,
            label_visibility="collapsed",
        )
        submit = st.button("Upload", disabled=not uploaded, type="primary")
        if submit and uploaded:
            dest = _safe_subdir(subdir.strip())
            if dest is None:
                st.error("Invalid subdirectory path.")
            else:
                os.makedirs(dest, exist_ok=True)
                saved = []
                for f in uploaded:
                    safe_name = Path(f.name).name
                    if not safe_name:
                        st.warning(f"Skipping file with invalid name: {f.name}")
                        continue
                    filepath = dest / safe_name
                    filepath.write_bytes(f.getvalue())
                    saved.append(str(filepath.relative_to(WORKSPACE_ROOT)))
                if saved:
                    st.success(f"Uploaded {len(saved)} file(s): {', '.join(saved)}")

    col1, col2 = st.columns([1, 8])
    with col1:
        if st.button("↺ Refresh", key="workspace_refresh"):
            st.session_state["workspace_iframe_v"] = (
                st.session_state.get("workspace_iframe_v", 0) + 1
            )
    with col2:
        st.link_button("Open in new tab", "/_/agent/workspace/")

    v = st.session_state.get("workspace_iframe_v", 0)
    components.iframe(f"/_/agent/workspace/?v={v}", height=1000, scrolling=True)
