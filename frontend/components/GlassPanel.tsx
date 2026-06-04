import {
  forwardRef,
  type ComponentPropsWithoutRef,
  type ElementType,
  type ForwardedRef,
  type ReactElement,
} from "react";
import { cn } from "@/lib/cn";

/**
 * `<GlassPanel>` — the **only** place in the codebase that owns the
 * Neural Dark "resting card" recipe. Phase 43: per the Claude-design
 * handoff, resting cards lean on the surface step (bg-2 over bg-0/bg-1)
 * + a subtle hairline border, NOT on glassmorphism — "no glassmorphism
 * on resting content cards". Floating elements (menus, modals) opt
 * into elevation via className.
 *
 * The component name keeps "Glass" for back-compat — every page imports
 * it under that name. The recipe itself dropped the backdrop-blur.
 */

const PANEL_CLASSES =
  // Surface step (bg-2) — opaque, sits one level above bg-0/bg-1 pages
  "bg-[var(--bg-2)] " +
  // 1px hairline per Neural Dark v2 §Borders
  "border border-[var(--border-subtle)] " +
  // lg radius (14px) per Neural Dark v2 §Cards
  "rounded-[14px]";

type PolymorphicProps<T extends ElementType> = {
  as?: T;
  className?: string;
} & Omit<ComponentPropsWithoutRef<T>, "as" | "className">;

type GlassPanelComponent = <T extends ElementType = "div">(
  props: PolymorphicProps<T> & { ref?: ForwardedRef<Element> },
) => ReactElement | null;

export const GlassPanel = forwardRef(function GlassPanel<
  T extends ElementType = "div",
>(
  { as, className, ...rest }: PolymorphicProps<T>,
  ref: ForwardedRef<Element>,
) {
  const Component = (as ?? "div") as ElementType;
  return (
    <Component ref={ref} className={cn(PANEL_CLASSES, className)} {...rest} />
  );
}) as GlassPanelComponent;

export default GlassPanel;
