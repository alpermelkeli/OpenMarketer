/**
 * A secret on its way into one request.
 *
 * TanStack Query keeps the variables of a mutation in its cache for as long as it
 * keeps the mutation, and shows them to anything that reads the cache. A mutation
 * that sends a secret is therefore given this holder instead of the text: the
 * request takes the value out, and what stays behind in the cache is empty.
 *
 * It is not encryption and it does not hide the value from the page's own code
 * while the person is typing it.
 */
export class OneTimeValue {
  #value: string | null;

  constructor(value: string) {
    this.#value = value;
  }

  /** The value, once. The holder forgets it, so a second request cannot be made from it. */
  take(): string {
    const value = this.#value;
    if (value === null) throw new Error("this value has already been sent");
    this.#value = null;
    return value;
  }
}
