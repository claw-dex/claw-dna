"""Tab: Workspace — file upload + iframe to Caddy file browser."""

import os
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components
from streamlit_chunk_file_uploader import uploader

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
            value="upload/",
            placeholder="e.g. data/input",
            help="Leave empty to upload to the workspace root.",
        )
        uploaded = st.file_uploader(
            "Choose files",
            accept_multiple_files=True,
            label_visibility="collapsed",
        )
        # Reveal the chunked-upload section if the user has selected any file
        # in the regular uploader that approaches Cloud Platform's request-body
        # limit. Threshold is conservative (50MB) since the proxy may block
        # before Streamlit ever sees the file.
        LARGE_FILE_THRESHOLD = 50 * 1024 * 1024
        if uploaded and any(
            getattr(f, "size", 0) >= LARGE_FILE_THRESHOLD for f in uploaded
        ):
            st.session_state["workspace_show_chunk"] = True

        submit = st.button("Upload", disabled=not uploaded, type="primary")
        # Manual fallback: files larger than the proxy limit are blocked
        # before Streamlit ever sees them, so .size detection won't fire.
        if not st.session_state.get("workspace_show_chunk"):
            if st.button(
                "My file is too large — show chunked uploader",
                key="workspace_show_chunk_btn",
            ):
                st.session_state["workspace_show_chunk"] = True
                st.rerun()
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

    if st.session_state.get("workspace_show_chunk"):
        with st.expander(
            "Upload large file (chunked)",
            icon=":material/cloud_upload:",
            expanded=True,
        ):
            st.caption(
                "Streamed in 16MB chunks to bypass Cloud Platform's request-body "
                "limit. One file at a time."
            )
            big_subdir = st.text_input(
                "Subdirectory (optional)",
                value="upload/",
                placeholder="e.g. data/input",
                help="Leave empty to upload to the workspace root.",
                key="workspace_big_subdir",
            )
            big_uploaded = uploader(
                "Choose a large file",
                key="workspace_chunk_uploader",
                chunk_size=16,
            )
            big_submit = st.button(
                "Upload large file",
                disabled=big_uploaded is None,
                type="primary",
                key="workspace_big_submit",
            )
            if big_submit and big_uploaded is not None:
                dest = _safe_subdir(big_subdir.strip())
                if dest is None:
                    st.error("Invalid subdirectory path.")
                else:
                    safe_name = Path(big_uploaded.name).name
                    if not safe_name:
                        st.warning(
                            f"Skipping file with invalid name: {big_uploaded.name}"
                        )
                    else:
                        os.makedirs(dest, exist_ok=True)
                        filepath = dest / safe_name
                        with st.spinner(f"Saving {safe_name}…"):
                            filepath.write_bytes(big_uploaded.read())
                        st.success(f"Uploaded: {filepath.relative_to(WORKSPACE_ROOT)}")

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
