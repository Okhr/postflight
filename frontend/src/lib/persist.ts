import { useCallback, useState } from "react";

/**
 * useState that survives a reload, kept in localStorage.
 *
 * For view preferences only: which curve is shown, which axes are hidden, what
 * playback speed. Never for content: the database owns that, and a stale copy in a
 * browser would be a second source of truth.
 *
 * Failures are swallowed on purpose. localStorage throws in private windows and
 * when the quota is full, and losing a preference must never take the page down.
 */
const PREFIX = "postflight:";

function read<T>(key: string, fallback: T): T {
  try {
    const raw = window.localStorage.getItem(PREFIX + key);
    return raw === null ? fallback : (JSON.parse(raw) as T);
  } catch {
    return fallback;
  }
}

export function usePersistentState<T>(key: string, fallback: T) {
  const [value, setValue] = useState<T>(() => read(key, fallback));

  const store = useCallback(
    (next: T | ((previous: T) => T)) => {
      setValue((previous) => {
        const resolved =
          typeof next === "function" ? (next as (p: T) => T)(previous) : next;
        try {
          window.localStorage.setItem(PREFIX + key, JSON.stringify(resolved));
        } catch {
          /* private window, or quota full */
        }
        return resolved;
      });
    },
    [key],
  );

  return [value, store] as const;
}

/**
 * Same, for a Set. JSON has no set type, so it travels as an array, which also
 * keeps what is written readable in devtools.
 */
export function usePersistentSet(key: string, fallback: string[] = []) {
  const [array, setArray] = usePersistentState<string[]>(key, fallback);
  const set = new Set(array);

  const toggle = useCallback(
    (member: string) => {
      setArray((previous) =>
        previous.includes(member)
          ? previous.filter((x) => x !== member)
          : [...previous, member],
      );
    },
    [setArray],
  );

  return [set, toggle] as const;
}
