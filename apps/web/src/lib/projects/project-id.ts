/** Whether a piece of an address can be a project's identifier, before it is put into a request. */

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function isProjectId(value: string): boolean {
  return UUID.test(value);
}

/** A chat's identifier is a UUID too, so it is checked the same way. */
export function isChatId(value: string): boolean {
  return UUID.test(value);
}

/** A research run's identifier is a UUID too, so it is checked the same way. */
export function isResearchRunId(value: string): boolean {
  return UUID.test(value);
}

/** A studio run's identifier is a UUID too, so it is checked the same way. */
export function isStudioRunId(value: string): boolean {
  return UUID.test(value);
}

/** A media asset's identifier, and each of its versions', is a UUID too. */
export function isMediaAssetId(value: string): boolean {
  return UUID.test(value);
}
