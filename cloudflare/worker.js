// Worker entry for the Cloudflare Containers deployment.
// Routes every request to a single container instance so the in-process
// pattern cache (digitize -> export/worksheet) stays consistent.
import { Container, getContainer } from "@cloudflare/containers";

export class StitchForgeContainer extends Container {
  defaultPort = 8000;
  sleepAfter = "15m";
}

export default {
  async fetch(request, env) {
    return getContainer(env.STITCHFORGE).fetch(request);
  },
};
