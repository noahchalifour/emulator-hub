// The agent flow through the TypeScript MCP SDK (what Claude Code and other
// Node agents use). Prints one JSON line per step for the pytest side to check.
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

const [url, token, profile] = process.argv.slice(2);
const transport = new StreamableHTTPClientTransport(new URL(url), {
  requestInit: { headers: { Authorization: `Bearer ${token}` } },
});
const client = new Client({ name: "e2e-ts", version: "1.0.0" });
await client.connect(transport);
const out = (step, value) => console.log(JSON.stringify({ step, value }));
out("instructions", client.getInstructions() ?? "");
out("tools", (await client.listTools()).tools.map((t) => t.name).sort());
const opts = { timeout: 600_000 };
let grant = (await client.callTool({ name: "acquire", arguments: { profile, holder: "e2e-ts", boot_wait_seconds: 0 } }, undefined, opts)).structuredContent;
out("acquire", grant);
const deadline = Date.now() + 600_000;
while (grant.state === "booting" && Date.now() < deadline) {
  await new Promise((r) => setTimeout(r, 2000));
  grant = (await client.callTool({ name: "heartbeat", arguments: { lease_id: grant.lease_id } })).structuredContent;
}
out("leased", grant);
const bad = await client.callTool({ name: "heartbeat", arguments: { lease_id: "nope" } });
out("error", { isError: bad.isError, text: bad.content?.[0]?.text });
out("release", (await client.callTool({ name: "release", arguments: { lease_id: grant.lease_id } })).structuredContent);
await client.close();
