/**
 * Which rush the interface is on, read from the URL.
 *
 * The URL is the selection: no shared state to keep in step, and a link to a rush
 * on a given step is a link anyone can paste. Read from the path rather than with
 * `useParams`, which inside a layout route sees the layout's own match and not the
 * `:id` of the child that carries it.
 */

/** The steps that act on one rush. Colour acts on a stabilized clip, so its `:id`
 *  is a render and must never be read as a rush. */
const RUSH_STEPS = ["derush", "stabilisation"] as const;

const RUSH_PATH = new RegExp(`^/(${RUSH_STEPS.join("|")})/(\\d+)`);

export function selectedRushId(pathname: string): number | null {
  const found = RUSH_PATH.exec(pathname);
  return found ? Number(found[2]) : null;
}

/** Where clicking a rush goes: the step being looked at, when it takes a rush at
 *  all, and derush otherwise. */
export function rushHref(pathname: string, id: number): string {
  const step = RUSH_STEPS.find((name) => pathname.startsWith(`/${name}`));
  return `/${step ?? "derush"}/${id}`;
}
