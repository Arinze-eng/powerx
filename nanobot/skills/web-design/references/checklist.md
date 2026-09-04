# Design QA checklist (run before shipping)

## Visual
- [ ] One clear focal point above the fold.
- [ ] Consistent spacing rhythm (multiples of 4/8).
- [ ] Exactly 1–2 fonts; type scale defined, not ad-hoc sizes.
- [ ] Palette limited: brand + neutrals + 1 accent.
- [ ] No pure #000/#fff for text/backgrounds on light mode.
- [ ] Buttons/cards share one radius & shadow system.
- [ ] Images have alt text and proper aspect ratios.

## Interaction
- [ ] Hover/focus/active states on every clickable element.
- [ ] Focus rings visible (keyboard nav works).
- [ ] Transitions ≤ 200ms, subtle (no jarring animations).
- [ ] Forms show validation/error states.

## Responsive
- [ ] Tested at 375px, 768px, 1024px, 1440px.
- [ ] No horizontal scroll on mobile.
- [ ] Tap targets ≥ 44px.
- [ ] Nav collapses to a menu on small screens.
- [ ] Text scales without overflow.

## Accessibility
- [ ] Color contrast AA (body ≥ 4.5:1, large ≥ 3:1).
- [ ] Semantic HTML (header/main/footer/nav/section).
- [ ] Headings in order h1→h2→h3.
- [ ] Form labels tied to inputs.
- [ ] Dark mode readable if supported.

## Polish
- [ ] Real copy, no lorem ipsum.
- [ ] Favicon + title + meta description set.
- [ ] Open Graph tags for social preview.
- [ ] Loading/empty/error states handled.
- [ ] Footer complete with links.