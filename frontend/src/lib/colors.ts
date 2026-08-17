/**
 * Colour tags for rushes.
 *
 * A fixed palette stored as a token, not a CSS value: the API keeps a word, the
 * front decides what it looks like. Six hues, told apart at a glance even in a
 * list of thirty — and the classes are spelled out here so Tailwind keeps them.
 */
export interface RushColor {
  token: string;
  label: string;
  dot: string;
  bar: string;
}

export const RUSH_COLORS: RushColor[] = [
  { token: "red", label: "Red", dot: "bg-red-500", bar: "bg-red-500" },
  { token: "amber", label: "Amber", dot: "bg-amber-500", bar: "bg-amber-500" },
  { token: "emerald", label: "Emerald", dot: "bg-emerald-500", bar: "bg-emerald-500" },
  { token: "sky", label: "Sky", dot: "bg-sky-500", bar: "bg-sky-500" },
  { token: "violet", label: "Violet", dot: "bg-violet-500", bar: "bg-violet-500" },
  { token: "pink", label: "Pink", dot: "bg-pink-500", bar: "bg-pink-500" },
];

/** The palette entry for a token, or null when the rush carries no tag. */
export function rushColor(token: string | null | undefined): RushColor | null {
  if (!token) return null;
  return RUSH_COLORS.find((color) => color.token === token) ?? null;
}
