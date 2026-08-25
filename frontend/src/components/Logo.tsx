/**
 * The mark, which is the app in one stroke: a shaky line that comes out flat.
 *
 * Same glyph as the favicon, on purpose. A tab and a sidebar showing two different
 * marks is how a product stops being recognisable, and this one has to read at 16 px
 * in a tab strip, which is why the stroke is thick and there is only one bump.
 *
 * Painted in the two theme tokens rather than in fixed black and white, so the badge
 * stays a badge whichever way round the theme is.
 */
export function Logo({ className = "h-5 w-5" }: { className?: string }) {
  return (
    <svg viewBox="0 0 32 32" className={className} aria-hidden="true">
      <rect width="32" height="32" rx="7" fill="hsl(var(--foreground))" />
      <path
        d="M4.5 16 q3 -9 6 0 t6 0 h11"
        fill="none"
        stroke="hsl(var(--background))"
        strokeWidth="3.2"
        strokeLinecap="round"
      />
    </svg>
  );
}
