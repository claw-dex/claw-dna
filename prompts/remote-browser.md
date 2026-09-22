# Remote Browser Access

When you are facing anti-bot measures website that blocking you from complete your tasks, **you MUST ask user to follow the instructions below to open a remote browser window for you to access**:

1. Ask user to open a remote browser window with the following command:

- On Mac OS: `/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222 --user-data-dir="/tmp/chrome_debug"`
- On Windows: `chrome.exe --remote-debugging-port=9222 --user-data-dir="C:\temp\chrome_debug"`

1. Once the user has started the remote browser, ask user to run the following command in the terminal to forward the remote debugging port via cloudflared tunnel: `cloudflared tunnel --url localhost:9222`
2. Wait for the user to share the generated public URL (e.g., `https://random-id.trycloudflare.com`), then ask the user to navigate to `http://localhost:9222/json/version` in their browser and share the JSON response with you.
3. Once the user sends you the JSON response, extract the `webSocketDebuggerUrl` field. It will contain a WebSocket URL using localhost and port 9222 (e.g., `ws://localhost:9222/devtools/browser/abc12345-53aa-42fa-a93c-f13b6fa8e2e0`). Replace `ws://localhost:9222` with `wss://{tunnel-hostname}` (e.g., `wss://random-id.trycloudflare.com/devtools/browser/abc12345-53aa-42fa-a93c-f13b6fa8e2e0`). Save this Secure WebSocket URL as `REMOTE_BROWSER_WSS_URL` using `keepass` skill for future use.
4. Use this WebSocket URL with your `agent-browser` skill to control the remote browser and bypass anti-bot measures by adding this additional argument `--cdp "{REMOTE_BROWSER_WS_URL}"` e.g: `agent-browser --cdp "wss://random-id.trycloudflare.com/devtools/browser/abc12345-53aa-42fa-a93c-f13b6fa8e2e0" snapshot` (`--profile` and `--cdp` are mutually exclusive, `--cdp` will use the remote browser profile instead of the local one)
5. Troubleshooting:
   - If you lose connection to the remote browser, ask the user to restart the remote browser and repeat steps 2-5 to get a new WebSocket URL, then update `REMOTE_BROWSER_WS_URL` in `keepass` with the new URL for future use. Example error message: `✗ Failed to connect via CDP to {REMOTE_BROWSER_WS_URL}. Make sure the app is running with --remote-debugging-port=...`
   - If the user reports the curl command fails, ask them to check that the cloudflared tunnel is still running and that Chrome was started with `--remote-debugging-port=9222`
   - **CAPTCHA challenges**: If you encounter a CAPTCHA while using the remote browser, attempt to solve it yourself first — use `agent-browser screenshot` to capture the page, analyze the CAPTCHA visually, and interact with it accordingly. If you cannot solve it after a reasonable attempt, inform the user that a CAPTCHA needs to be solved manually. Since the remote browser is running on the user's local machine, ask the user to solve the CAPTCHA directly in the Chrome window on their machine, then notify you once it's done so you can continue.

**Prequisite**: The user must have `cloudflared` and `Chrome` installed on their local machine to set up the remote browser access. Show <https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/downloads/> to user for `cloudflared` installation instructions.
Before asking the user to set up remote browser access, if `REMOTE_BROWSER_WSS_URL` is already set in `keepass`, try using it first to connect via CDP. If the connection fails, then proceed with the instructions above to set up a new remote browser session and update `REMOTE_BROWSER_WSS_URL` with the new WebSocket URL.
Store args example: `--title "REMOTE_BROWSER_WSS_URL" --username "" --password "" --url "wss://random-id.trycloudflare.com/devtools/browser/abc12345-53aa-42fa-a93c-f13b6fa8e2e0" --group "System"`
