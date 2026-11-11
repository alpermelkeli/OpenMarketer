/**
 * A step a page takes once, however often React runs the effect that takes it. In development
 * React mounts a component, runs its effects, and runs them again on the same instance, so a
 * request sent from an effect would go twice; a ref given to this keeps the second from sending.
 */

/** True the first time for this holder, false ever after. */
export function claimOnce(holder: { current: boolean }): boolean {
  if (holder.current) return false;
  holder.current = true;
  return true;
}
