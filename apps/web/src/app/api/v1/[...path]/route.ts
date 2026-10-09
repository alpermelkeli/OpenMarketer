import { proxyToApi } from "@/lib/server/api-proxy";

/**
 * The browser's only way to the OpenMarketer API. The rules are in
 * `lib/server/api-proxy.ts`; this file wires them to the route and the environment.
 */
async function handle(request: Request, context: RouteContext<"/api/v1/[...path]">) {
  const { path } = await context.params;
  return proxyToApi(request, path, process.env.OPENMARKETER_API_URL);
}

export { handle as GET, handle as POST };
