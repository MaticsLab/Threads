// Worker entry for the Cloudflare Containers deployment.
//
// /static/* is answered from Cloudflare's edge (Workers static assets);
// everything else goes to a single container instance so the in-process
// pattern cache (digitize -> export/worksheet) stays consistent.
import { Container, getContainer } from "@cloudflare/containers";

export class StitchForgeContainer extends Container {
  defaultPort = 8000;
  // jobs live in the container's memory/tmp; keep it warm for a working
  // session so preview/export/worksheet links stay valid between clicks
  sleepAfter = "1h";
  // AI layer naming: set the secret with `npx wrangler secret put ANTHROPIC_API_KEY`
  envVars = { ANTHROPIC_API_KEY: this.env.ANTHROPIC_API_KEY ?? "" };
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname.startsWith("/static/")) {
      const assetUrl = new URL(url);
      assetUrl.pathname = url.pathname.slice("/static".length);
      return env.ASSETS.fetch(new Request(assetUrl, request));
    }
    return getContainer(env.STITCHFORGE).fetch(request);
  },
};
