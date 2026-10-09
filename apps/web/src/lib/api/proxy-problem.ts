/**
 * Errors the dashboard's own API proxy answers with, before or instead of the API.
 *
 * They share the API's `{code, message}` shape so the browser handles one error
 * format, but the codes are the proxy's: the API never sends them.
 */

export const PROXY_ERROR_CODES = [
  "proxy_request_refused",
  "proxy_route_not_allowed",
  "proxy_body_rejected",
  "api_unreachable",
  "proxy_misconfigured",
] as const;

export type ProxyErrorCode = (typeof PROXY_ERROR_CODES)[number];

export type ProxyProblem = {
  code: ProxyErrorCode;
  message: string;
};
