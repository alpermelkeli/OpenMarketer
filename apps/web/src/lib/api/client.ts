/**
 * The typed client for the OpenMarketer API, as the browser reaches it: through the
 * dashboard's own proxy under `/api`, never the API's address directly.
 */

import createClient from "openapi-fetch";

import { apiErrorFrom } from "./api-error";
import type { paths } from "./schema";

export const api = createClient<paths>({ baseUrl: "/api" });

type ApiResult<Data> = { data?: Data; error?: unknown; response: Response };

/** The data of a successful call; an `ApiError` is thrown for every other answer. */
export function dataOf<Data>(result: ApiResult<Data>): Data {
  if (result.response.ok && result.data !== undefined) return result.data;
  throw apiErrorFrom(result.response.status, result.error);
}
