/**
 * The palette a folder's dot is drawn from.
 *
 * A fixed palette stored as a token, not a CSS value: the API keeps a word, the
 * front decides what it looks like. Six hues, told apart at a glance even in a
 * list of thirty, and the classes are spelled out here so Tailwind keeps them.
 */
export interface FolderColor {
  token: string;
  label: string;
  dot: string;
  bar: string;
}

export const FOLDER_COLORS: FolderColor[] = [
  { token: "red", label: "Red", dot: "bg-red-500", bar: "bg-red-500" },
  { token: "amber", label: "Amber", dot: "bg-amber-500", bar: "bg-amber-500" },
  { token: "emerald", label: "Emerald", dot: "bg-emerald-500", bar: "bg-emerald-500" },
  { token: "sky", label: "Sky", dot: "bg-sky-500", bar: "bg-sky-500" },
  { token: "violet", label: "Violet", dot: "bg-violet-500", bar: "bg-violet-500" },
  { token: "pink", label: "Pink", dot: "bg-pink-500", bar: "bg-pink-500" },
];

/** The palette entry for a token, or null when the token is not one of ours. */
export function folderColor(token: string | null | undefined): FolderColor | null {
  if (!token) return null;
  return FOLDER_COLORS.find((color) => color.token === token) ?? null;
}
